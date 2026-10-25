"""
compile.py - run vbsp -> vvis -> vrad from a game's own bin folder and drop the .bsp into maps/.

Needs the compile tools that ship with the game (HL2 / Portal / Garry's Mod include vbsp.exe,
vvis.exe, vrad.exe in their bin folder, or use Source SDK Base 2013 Singleplayer's bin).
"""

import logging
import os
import shutil
import subprocess
from typing import List, Optional

log = logging.getLogger("interloper.mapgen")
EXE = ".exe" if os.name == "nt" else ""


class CompileError(RuntimeError):
    pass


def find_bindir(gamedir: str, bindir: str = "") -> str:
    """Folder containing vbsp: explicit one, else search around the game folder."""
    cands: List[str] = []
    if bindir:
        cands.append(bindir)
    g = os.path.abspath(gamedir)
    for up in (".", "..", os.path.join("..", ".."), os.path.join("..", "..", "..")):
        base = os.path.normpath(os.path.join(g, up))
        cands += [os.path.join(base, "bin"), os.path.join(base, "bin", "win64"), os.path.join(base, "bin", "x64")]
    cands += [r"C:\Program Files (x86)\Steam\steamapps\common\Source SDK Base 2013 Singleplayer\bin"]
    for c in cands:
        if os.path.exists(os.path.join(c, "vbsp" + EXE)):
            return c
    raise CompileError("could not find vbsp" + EXE + " - pass --bindir <folder containing vbsp/vvis/vrad>")


def _run(cmd: List[str], cwd: str, log_lines: List[str], step: str):
    log.info("%s: %s", step, " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    out = []
    for line in proc.stdout:
        out.append(line.rstrip())
    proc.wait()
    log_lines.extend(out)
    text = "\n".join(out)
    if proc.returncode != 0:
        raise CompileError(f"{step} failed (exit {proc.returncode}); last output:\n" + "\n".join(out[-12:]))
    if step == "vbsp" and ("leaked" in text.lower()):
        raise CompileError("vbsp reports a leak:\n" + "\n".join(l for l in out if "leak" in l.lower())[:600])
    return text


def compile_map(vmf_path: str, gamedir: str, bindir: str = "", fast: bool = True,
                install: bool = True, vis: bool = True, light: bool = True) -> dict:
    gamedir = os.path.abspath(gamedir)
    if not os.path.exists(os.path.join(gamedir, "gameinfo.txt")):
        raise CompileError(f"no gameinfo.txt in {gamedir} - that is not a game folder")
    bindir = find_bindir(gamedir, bindir)
    base = os.path.splitext(os.path.abspath(vmf_path))[0]
    cwd = os.path.dirname(base)
    log_lines: List[str] = []
    tool = lambda n: os.path.join(bindir, n + EXE)

    _run([tool("vbsp"), "-game", gamedir, base], cwd, log_lines, "vbsp")
    if vis:
        _run([tool("vvis"), "-game", gamedir] + (["-fast"] if fast else []) + [base], cwd, log_lines, "vvis")
    if light:
        _run([tool("vrad"), "-game", gamedir] + (["-fast"] if fast else ["-final"]) + [base], cwd, log_lines, "vrad")
    bsp = base + ".bsp"
    if not os.path.exists(bsp):
        raise CompileError("compiler finished but no .bsp was written")
    out = {"bsp": bsp, "log": log_lines, "installed": None}
    if install:
        maps = os.path.join(gamedir, "maps")
        os.makedirs(maps, exist_ok=True)
        dest = os.path.join(maps, os.path.basename(bsp))
        shutil.copy2(bsp, dest)
        out["installed"] = dest
    return out
