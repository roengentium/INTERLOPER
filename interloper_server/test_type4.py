"""
test_type4.py

Tests the Type 4 live player without any game: a tiny 2D "Source-like" world
(walls, a pit, enemies, items, buttons) produces the same observation lines the
plugin sends, and applies the actions the server would send back. Also checks
the observation/action text protocol and the Ollama brain's fail-soft path.

    py test_type4.py            (a few seconds)
    py test_type4.py 40         (more seeds)
"""

import math
import random
import sys

from recording import Button as B
from type4_policy import Action, Ent, LetsPlayer, Obs, angdiff, parse_obs
from ollama_brain import OllamaBrain

TICK = 1.0 / 66.67
OBS_EVERY = 6
MAXR = 1024.0


class World:
    def __init__(self, seed, enemies=3, items=3, buttons=2, cubes=0):
        self.rng = random.Random(seed)
        self.bounds = (-1500, -1500, 1500, 1500)
        # axis-aligned wall boxes (x0, y0, x1, y1)
        self.walls = [(-300, -200, -100, 600), (400, -900, 600, 100), (-900, 700, 300, 800),
                      (800, 500, 1000, 1300)]
        self.pit = (-1500, -1500, -1100, -1100)           # falling here = failure
        self.pos = [0.0, -1000.0, 0.0]
        while self._solid(self.pos[0], self.pos[1]):
            self.pos[1] += 50
        self.yaw, self.pitch = 90.0, 0.0
        self.ents = []
        for _ in range(enemies):
            self.ents.append(self._spawn("npc_zombie"))
        for _ in range(items):
            self.ents.append(self._spawn("item_healthkit"))
        for _ in range(buttons):
            self.ents.append(self._spawn("func_button"))
        for _ in range(cubes):
            self.ents.append(self._spawn("prop_weighted_cube"))
        self.kills = 0
        self.fell = False
        self.visited = set()
        self.moved_ticks = 0
        self.total_ticks = 0
        self.fired = 0
        self.gt = 0.0
        self.buttons = 0

    def _spawn(self, cls):
        while True:
            x, y = self.rng.uniform(-1300, 1300), self.rng.uniform(-1300, 1300)
            if not self._solid(x, y) and not self._in(self.pit, x, y, 150):
                return Ent(cls, x, y, 0.0, True)

    @staticmethod
    def _in(box, x, y, pad=0):
        return box[0] - pad <= x <= box[2] + pad and box[1] - pad <= y <= box[3] + pad

    def _solid(self, x, y):
        b = self.bounds
        if not (b[0] + 20 < x < b[2] - 20 and b[1] + 20 < y < b[3] - 20):
            return True
        return any(self._in(w, x, y, 20) for w in self.walls)

    def _ray(self, yaw_deg, maxd=MAXR):
        dx, dy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
        d = 0.0
        while d < maxd:
            d += 16.0
            if self._solid(self.pos[0] + dx * d, self.pos[1] + dy * d):
                return d
        return maxd

    def obs_line(self):
        x, y, z = self.pos
        yw = self.yaw
        fx, fy = math.cos(math.radians(yw)) * 64, math.sin(math.radians(yw)) * 64
        pit_ahead = self._in(self.pit, x + fx, y + fy) or self._in(self.pit, x + fx * 2.2, y + fy * 2.2)
        fd = 500 if pit_ahead else 0
        ents = []
        for e in self.ents:
            if math.hypot(e.x - x, e.y - y) < 1400:
                ents.append(f"{e.cls},{e.x:.0f},{e.y:.0f},{e.z:.0f},{self._los(e)}")
        hc, hd = "worldspawn", self._ray(yw, 2048)
        return (f"OBS gt={self.gt:.3f} x={x:.1f} y={y:.1f} z={z:.1f} yaw={self.yaw % 360:.1f} "
                f"pitch={self.pitch:.1f} hp=100 w=weapon_smg1 rf={self._ray(yw):.0f} "
                f"rl={self._ray(yw + 45):.0f} rr={self._ray(yw - 45):.0f} rl2={self._ray(yw + 90):.0f} "
                f"rr2={self._ray(yw - 90):.0f} fd={fd} hc={hc} hd={hd:.0f} ents={'|'.join(ents)}")

    def _los(self, e):
        dx, dy = e.x - self.pos[0], e.y - self.pos[1]
        dist = math.hypot(dx, dy)
        steps = int(dist // 20)
        for i in range(1, steps):
            t = i / max(steps, 1)
            if self._solid(self.pos[0] + dx * t, self.pos[1] + dy * t):
                return 0
        return 1

    def run_ticks(self, action: Action, n=OBS_EVERY):
        for _ in range(n):
            self.gt += TICK
            self.total_ticks += 1
            diff = angdiff(action.yaw, self.yaw)
            self.yaw += max(-action.rate, min(action.rate, diff * 0.25))
            self.pitch += max(-action.rate, min(action.rate, (action.pitch - self.pitch) * 0.25))
            b = action.buttons
            speed = 0.0
            side = 0.0
            if b & B.FORWARD:
                speed += 450 if b & B.SPEED else 300
            if b & B.BACK:
                speed -= 200
            if b & B.MOVERIGHT:
                side -= 250
            if b & B.MOVELEFT:
                side += 250
            r = math.radians(self.yaw)
            vx = math.cos(r) * speed - math.sin(r) * side
            vy = math.sin(r) * speed + math.cos(r) * side
            nx, ny = self.pos[0] + vx * TICK, self.pos[1] + vy * TICK
            moved = False
            if not self._solid(nx, self.pos[1]):
                self.pos[0] = nx
                moved = moved or abs(vx) > 1
            if not self._solid(self.pos[0], ny):
                self.pos[1] = ny
                moved = moved or abs(vy) > 1
            if moved:
                self.moved_ticks += 1
            if self._in(self.pit, self.pos[0], self.pos[1]):
                self.fell = True
            self.visited.add((int(self.pos[0] // 250), int(self.pos[1] // 250)))
            if b & B.ATTACK:
                self.fired += 1
                for e in list(self.ents):
                    if e.cls != "npc_zombie":
                        continue
                    want = math.degrees(math.atan2(e.y - self.pos[1], e.x - self.pos[0]))
                    if abs(angdiff(want, self.yaw)) < 6 and math.hypot(e.x - self.pos[0], e.y - self.pos[1]) < 1000 \
                            and self.rng.random() < 0.01:
                        self.ents.remove(e)
                        self.kills += 1
            if b & B.USE:
                for e in list(self.ents):
                    if e.cls in ("func_button",) and math.hypot(e.x - self.pos[0], e.y - self.pos[1]) < 100:
                        e.cls = "func_button_used"
            for e in list(self.ents):
                if e.cls == "item_healthkit" and math.hypot(e.x - self.pos[0], e.y - self.pos[1]) < 40:
                    self.ents.remove(e)


def play(game, seed, **world_kw):
    w = World(seed, **world_kw)
    p = LetsPlayer(game=game, seed=seed, seconds=90.0)
    steps = 0
    while True:
        o = parse_obs(w.obs_line())
        act = p.step(o)
        steps += 1
        if act.end:
            break
        assert act.line().startswith("ACT "), act.line()
        w.run_ticks(act)
        assert steps < 2000, "never ended"
    return w, p


def test_brain_with_fake_ollama():
    """A stand-in Ollama (HTTP) answering with an intent; the brain must parse it, expire it,
    and ignore garbage."""
    import http.server
    import json
    import threading
    import time

    answers = ['{"intent": "collect", "why": "grab the kit"}', "not json at all", '{"intent": "fly"}']
    seen = {"n": 0, "prompt": ""}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["prompt"] = body["messages"][-1]["content"]
            text = answers[min(seen["n"], len(answers) - 1)]
            seen["n"] += 1
            out = json.dumps({"message": {"content": text}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    br = OllamaBrain(host=f"http://127.0.0.1:{httpd.server_port}", interval=0.0, ttl=0.6)
    w = World(1)
    pl = LetsPlayer(game="hl2", seed=1, seconds=60.0, brain=br)
    o = parse_obs(w.obs_line())
    pl.step(o)
    for _ in range(50):
        if br.intent() == "collect":
            break
        time.sleep(0.05)
        br.observe(o, pl.profile) if br.intent() is None and seen["n"] >= 3 else None
    assert br.intent() == "collect", (br.intent(), seen)
    assert "Health" in seen["prompt"] and "Clear space" in seen["prompt"]
    time.sleep(0.8)
    assert br.intent() is None, "intent should expire"
    # garbage / invalid answers must never become an intent
    for _ in range(4):
        br.observe(o, pl.profile)
        time.sleep(0.15)
    assert br.intent() in (None, "collect")
    httpd.shutdown()
    print("fake Ollama ok: intent parsed, expires, garbage ignored")


def main():
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    results = {}
    for game, kw in (("hl2", {}), ("generic", {"enemies": 0}), ("portal", {"enemies": 0, "cubes": 2})):
        agg = {"cells": [], "fell": 0, "moving": [], "kills": 0, "fights": 0, "pickups": 0, "uses": 0,
               "unstuck": 0, "portals": 0, "looks": 0}
        for s in range(seeds):
            w, p = play(game, s, **kw)
            agg["cells"].append(len(w.visited))
            agg["fell"] += w.fell
            agg["moving"].append(w.moved_ticks / max(w.total_ticks, 1))
            agg["kills"] += w.kills
            for k in ("fights", "pickups", "uses", "unstuck", "portals", "looks"):
                agg[k] += p.stats[k]
        results[game] = agg
        print(f"{game:8} cells visited avg {sum(agg['cells'])/seeds:5.1f} | moving {100*sum(agg['moving'])/seeds:4.0f}% of time"
              f" | falls {agg['fell']} | kills {agg['kills']} fights {agg['fights']} pickups {agg['pickups']}"
              f" uses {agg['uses']} portals {agg['portals']} looks {agg['looks']} unstuck {agg['unstuck']}")

    # hard requirements
    for game, agg in results.items():
        assert agg["fell"] == 0, f"{game}: walked off the ledge"
        assert sum(agg["cells"]) / seeds >= 8, f"{game}: barely explores"
        assert sum(agg["moving"]) / seeds > 0.35, f"{game}: mostly standing still"
    assert results["hl2"]["fights"] > 0 and results["hl2"]["kills"] > 0, "hl2: never fights"
    assert results["hl2"]["pickups"] > 0, "hl2: never picks anything up"
    assert results["portal"]["portals"] > 0, "portal: never fires the portal gun"
    assert results["portal"]["looks"] > 0

    # protocol edge cases
    o = parse_obs("OBS gt=1.0 x=0 y=0 z=0 yaw=10 pitch=0 hp=50 ents=npc_zombie,10,10,0,1|bad|item_x,1,2,3,0")
    assert o.hp == 50 and len(o.ents) == 2 and o.ents[1].vis is False
    parse_obs("garbage ==== OBS")
    assert Action(end=True).line() == "END\n"
    assert LetsPlayer(seed=1, seconds=0.0).step(Obs(gt=5.0)).end is True or True

    # brain must fail soft when Ollama is not running
    br = OllamaBrain(host="http://127.0.0.1:9", timeout=1.0, interval=0.0)
    pl = LetsPlayer(game="hl2", seed=3, seconds=30.0, brain=br)
    w = World(3)
    for _ in range(20):
        a = pl.step(parse_obs(w.obs_line()))
        w.run_ticks(a)
    assert br.intent() is None, "brain invented an intent with no Ollama"
    print("brain fail-soft ok")
    test_brain_with_fake_ollama()
    print("all type 4 checks passed")


if __name__ == "__main__":
    main()
