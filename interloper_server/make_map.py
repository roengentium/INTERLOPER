"""
make_map.py - generate a Source map (VMF) and optionally compile it to a playable .bsp.

  py make_map.py --game hl2 --seed 7 --compile
  py make_map.py --game portal --mode theme --hint "empty test chamber wing, nobody home" --ollama llama3 --compile
  py make_map.py --game garrysmod --mode llm --llm-url https://<provider>/v1 --llm-model <model> --compile

Modes: procedural (code only) | theme (code builds it, the AI picks the mood) | llm (the AI writes the VMF,
checked + repaired, falls back to procedural) | auto (theme if an AI is configured, else procedural).
Then in game:  map <name printed at the end>
"""

import argparse
import json
import logging
import os
import sys

from mapgen.build import build
from mapgen.compile import CompileError, compile_map
from mapgen.llm import LLMClient
from mapgen.themes import GAME_FAMILY, SIZES, styles

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("interloper.mapgen")


def load_config() -> dict:
    path = os.path.join(HERE, "mapgen_config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def run(argv, client=None) -> str:
    """Do the work; returns a human-readable summary. `client` lets the server pass its own LLM."""
    ap = argparse.ArgumentParser(prog="make_map", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True, choices=sorted(set(GAME_FAMILY) | {"garrysmod"}))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--mode", default="auto", choices=("auto", "procedural", "theme", "llm"))
    ap.add_argument("--size", default="", choices=("",) + tuple(SIZES))
    ap.add_argument("--style", default="")
    ap.add_argument("--hint", default="", help="mood for the AI, e.g. 'flooded maintenance wing, one light still on'")
    ap.add_argument("--tag", default="FSKY_SVR_GEN", help="lore tag written into the map (worldspawn comment + info_target)")
    ap.add_argument("--name", default="", help="map name (default: <tag>_<seed>, lowercase)")
    ap.add_argument("--out", default=os.path.join(HERE, "maps_out"))
    ap.add_argument("--compile", action="store_true", help="run vbsp/vvis/vrad and install the .bsp")
    ap.add_argument("--gamedir", default="", help="folder containing gameinfo.txt (default from mapgen_config.json)")
    ap.add_argument("--bindir", default="", help="folder containing vbsp/vvis/vrad")
    ap.add_argument("--final", action="store_true", help="slow, higher quality vrad (default is -fast)")
    ap.add_argument("--no-install", action="store_true", help="compile but don't copy into the game's maps folder")
    ap.add_argument("--ollama", default=None)
    ap.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    ap.add_argument("--llm-url", default=None)
    ap.add_argument("--llm-model", default=None)
    a = ap.parse_args(argv)

    client = client or LLMClient.from_args(a.ollama, a.ollama_host, a.llm_url, a.llm_model)
    res = build(a.game, a.seed, a.mode, a.size, a.style, a.hint, a.tag, client)
    name = (a.name or f"{a.tag}_{res.seed}").lower()
    name = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)[:31]

    os.makedirs(a.out, exist_ok=True)
    vmf_path = os.path.join(a.out, name + ".vmf")
    with open(vmf_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(res.vmf_text)
    lines = [f"mode: {res.mode_used} | style {res.theme.style} | {res.theme.cols}x{res.theme.rows} rooms | "
             f"{res.report.brushes} brushes | sealed ok"]
    lines += [f"note: {n}" for n in res.notes]
    lines.append(f"wrote {vmf_path}")

    if a.compile:
        cfg = load_config().get("garrysmod" if a.game == "gmod" else a.game, {})
        gamedir = a.gamedir or cfg.get("gamedir", "")
        if not gamedir:
            lines.append("not compiled: no --gamedir and nothing in mapgen_config.json for this game")
        else:
            try:
                out = compile_map(vmf_path, gamedir, a.bindir or cfg.get("bindir", ""), fast=not a.final,
                                  install=not a.no_install)
                lines.append(f"compiled {out['bsp']}")
                if out["installed"]:
                    lines.append(f"installed {out['installed']}")
                    lines.append(f"in game:  map {name}")
            except CompileError as exc:
                lines.append(f"COMPILE FAILED: {exc}")
    else:
        lines.append("not compiled (add --compile). To do it by hand open the .vmf in Hammer, or run vbsp/vvis/vrad on it.")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    print(run(sys.argv[1:]))
