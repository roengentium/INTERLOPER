"""Map generator tests: geometry, leak detection, the three AI modes (fake LLM) and the compile runner."""
import http.server, json, os, re, stat, tempfile, threading, time

import make_map
from mapgen import validate as V
from mapgen.build import build
from mapgen.compile import CompileError, compile_map
from mapgen.layout import generate
from mapgen.llm import LLMClient
from mapgen.themes import SIZES, Theme, sanitize, styles, from_dict
from mapgen.vmf import VMF


def test_geometry():
    a = build("hl2", 3, "procedural").vmf_text
    assert a == build("hl2", 3, "procedural").vmf_text, "same seed must give the same map"
    assert a != build("hl2", 4, "procedural").vmf_text
    n = 0
    for game in ("hl2", "portal", "garrysmod"):
        for st in styles(game):
            for size in ("tiny", "small", "medium"):
                r = build(game, 7 + n, "procedural", size, st)
                assert r.report.ok and not r.report.warnings, (game, st, size, r.report.errors, r.report.warnings)
                n += 1
    root = V.parse_vmf(a)
    assert len(root.all("world")) == 1 and root.all("entity")
    print(f"geometry ok: {n} game/style/size combos sealed, deterministic")


def test_leak_detection():
    t = sanitize(Theme(), "hl2")
    vmf, info = generate(t, "hl2", 5)
    assert V.check(vmf.text()).ok
    # knock a hole in the outer wall: drop the first world brush that touches the map edge
    root = V.parse_vmf(vmf.text())
    world = root.all("world")[0]
    victims = [s for s in world.all("solid") if V.Brush(s).mins[0] <= -32 + 0.1 and V.Brush(s).maxs[2] == t.height
               and V.Brush(s).mins[2] == 0 and V.Brush(s).maxs[0] <= 0]
    assert victims, "no perimeter wall found"
    # remove one from the middle of the left edge (not a corner post) so the hole is real
    mid = sorted(victims, key=lambda s: V.Brush(s).mins[1])[len(victims) // 2]
    world.kids.remove(mid)
    rep = V.check(V.dump(root))
    assert rep.leaked and not rep.ok, rep
    print("leak detection ok")


def fake_llm(replies):
    seen = {"n": 0, "prompts": []}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["prompts"].append(body["messages"][-1]["content"])
            txt = replies[min(seen["n"], len(replies) - 1)]
            seen["n"] += 1
            out = json.dumps({"choices": [{"message": {"content": txt}}], "message": {"content": txt}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)
        def log_message(self, *a): pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, seen


def test_theme_mode():
    reply = ('Here you go: {"name":"Flooded Wing","style":"nonsense","size":"large","height":999,'
             '"darkness":5,"light_rgb":[999,-4,128],"fog":true,"fog_end":1,"items":500}')
    httpd, seen = fake_llm([reply])
    for kind, host in (("openai", f"http://127.0.0.1:{httpd.server_port}/v1"), ("ollama", f"http://127.0.0.1:{httpd.server_port}")):
        c = LLMClient(kind, host, "m", "k", timeout=5)
        r = build("hl2", 1, "theme", hint="flooded wing", client=c)
        th = r.theme
        assert r.mode_used == "theme" and r.report.ok
        assert (th.cols, th.rows) == SIZES["large"] and th.style == "dev"
        assert th.height == 384 and th.darkness == 0.9 and th.items == 30 and th.fog_end == 400
        assert th.light_rgb == (255, 0, 128), th.light_rgb
        assert "flooded wing" in seen["prompts"][-1]
    httpd.shutdown()
    # dead LLM -> random theme, still a valid map
    dead = LLMClient("openai", "http://127.0.0.1:9/v1", "m", timeout=1)
    r = build("portal", 2, "theme", client=dead)
    assert r.report.ok and any("theme model failed" in n for n in r.notes), r.notes
    # no LLM configured: theme degrades to procedural with a note
    r = build("hl2", 2, "theme", client=None)
    assert r.mode_used == "procedural" and any("needs an LLM" in n for n in r.notes)
    print("theme mode ok (clamping, both API kinds, dead LLM, no LLM)")


def room_vmf(inverted=False, leaky=False):
    """A valid sealed 512 cube room, written the way a model would (optionally with inverted winding / a hole)."""
    v = VMF()
    m = "DEV/DEV_MEASUREWALL01A"
    v.brush((-288, -288, -32), (288, 288, 0), m)
    v.brush((-288, -288, 256), (288, 288, 288), m)
    v.brush((-288, -288, 0), (-256, 288, 256), m)
    v.brush((256, -288, 0), (288, 288, 256), m)
    v.brush((-256, -288, 0), (256, -256, 256), m)
    if not leaky:
        v.brush((-256, 256, 0), (256, 288, 256), m)
    v.entity("info_player_start", origin="0 0 8")
    v.entity("light", origin="0 0 200", _light="255 255 255 200")
    text = v.text()
    if inverted:
        def flip(mo):
            p = re.findall(r"\([^)]*\)", mo.group(1))
            return f'"plane" "{p[0]} {p[2]} {p[1]}"'
        text = re.sub(r'"plane" "([^"]*)"', flip, text)
    return text


def test_llm_vmf_mode():
    # a) valid but inverted winding -> repaired and accepted
    httpd, seen = fake_llm(["```vmf\n" + room_vmf(inverted=True) + "\n```"])
    c = LLMClient("openai", f"http://127.0.0.1:{httpd.server_port}/v1", "m", timeout=10)
    r = build("hl2", 1, "llm", client=c)
    assert r.mode_used == "llm" and r.report.ok, (r.notes, r.report.errors)
    assert any("re-wound" in n for n in r.notes)
    httpd.shutdown()
    # b) leaky map twice -> rejected, retry prompt mentions the leak, falls back to procedural
    httpd, seen = fake_llm([room_vmf(leaky=True)])
    c = LLMClient("openai", f"http://127.0.0.1:{httpd.server_port}/v1", "m", timeout=10)
    r = build("hl2", 1, "llm", client=c)
    assert r.mode_used != "llm" and r.report.ok and any("rejected" in n for n in r.notes), r.notes
    assert any("LEAK" in p for p in seen["prompts"]), "model should be told why it was rejected"
    httpd.shutdown()
    # c) garbage
    httpd, _ = fake_llm(["I cannot do that, sorry."])
    c = LLMClient("openai", f"http://127.0.0.1:{httpd.server_port}/v1", "m", timeout=10)
    r = build("portal", 1, "llm", client=c)
    assert r.mode_used != "llm" and r.report.ok
    httpd.shutdown()
    print("llm-vmf mode ok (repair, leak rejection with feedback, garbage fallback)")


def test_compile_runner():
    tmp = tempfile.mkdtemp()
    game = os.path.join(tmp, "game"); os.makedirs(game)
    open(os.path.join(game, "gameinfo.txt"), "w").write("x")
    bindir = os.path.join(tmp, "bin"); os.makedirs(bindir)
    logf = os.path.join(tmp, "calls.txt")
    for tool in ("vbsp", "vvis", "vrad"):
        p = os.path.join(bindir, tool)
        open(p, "w").write(f'#!/bin/sh\necho "{tool} $@" >> "{logf}"\n'
                           f'if [ "{tool}" = "vbsp" ]; then eval last=\\${{$#}}; echo fake > "$last.bsp"; fi\n'
                           f'if [ -n "$FAKE_LEAK" ] && [ "{tool}" = "vbsp" ]; then echo "**** leaked ****"; fi\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    vmf = os.path.join(tmp, "m.vmf"); open(vmf, "w").write("x")
    out = compile_map(vmf, game, bindir)
    calls = open(logf).read().splitlines()
    assert [c.split()[0] for c in calls] == ["vbsp", "vvis", "vrad"], calls
    assert "-game" in calls[0] and calls[1].count("-fast") == 1 and calls[2].count("-fast") == 1
    assert os.path.exists(os.path.join(game, "maps", "m.bsp")) and out["installed"]
    os.environ["FAKE_LEAK"] = "1"
    try:
        compile_map(vmf, game, bindir)
        assert False, "leak must raise"
    except CompileError as exc:
        assert "leak" in str(exc).lower()
    del os.environ["FAKE_LEAK"]
    try:
        compile_map(vmf, tmp, bindir)
        assert False
    except CompileError as exc:
        assert "gameinfo" in str(exc)
    print("compile runner ok (order, args, install, leak + bad gamedir errors)")


def test_cli_and_server_command():
    tmp = tempfile.mkdtemp()
    s = make_map.run(["--game", "portal", "--seed", "9", "--out", tmp, "--name", "My Map!"])
    assert os.path.exists(os.path.join(tmp, "my_map_.vmf")), os.listdir(tmp)
    assert "sealed ok" in s
    import server
    srv = server.InterloperServer()
    msg = srv.run_console_command(f"map hl2 --seed 3 --out {tmp}")
    assert "background" in msg
    for _ in range(100):
        if os.path.exists(os.path.join(tmp, "fsky_svr_gen_3.vmf")):
            break
        time.sleep(0.1)
    assert os.path.exists(os.path.join(tmp, "fsky_svr_gen_3.vmf"))
    assert "usage" in srv.run_console_command("map")
    print("cli + server 'map' command ok")


if __name__ == "__main__":
    test_geometry(); test_leak_detection(); test_theme_mode(); test_llm_vmf_mode()
    test_compile_runner(); test_cli_and_server_command()
    print("all mapgen checks passed")
