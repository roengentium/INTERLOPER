"""OpenAI-compatible brain against a fake /v1/chat/completions server."""
import http.server, json, threading, time
from llm_brain import OpenAIBrain
from type4_policy import LetsPlayer, parse_obs

seen = {"auth": None, "path": None, "n": 0}
answers = ['Sure! ```json\n{"intent": "use", "why": "press the button"}\n```', 'no json', '{"intent":"fly"}']


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen["auth"], seen["path"], seen["model"] = self.headers.get("Authorization"), self.path, body["model"]
        txt = answers[min(seen["n"], len(answers) - 1)]
        seen["n"] += 1
        out = json.dumps({"choices": [{"message": {"content": txt}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)
    def log_message(self, *a): pass


httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
br = OpenAIBrain(f"http://127.0.0.1:{httpd.server_port}/v1", "gpt-x", "sk-test", interval=0.0, ttl=5.0)
pl = LetsPlayer(game="portal", seed=1, seconds=60.0, brain=br)
o = parse_obs("OBS gt=1 x=0 y=0 z=0 yaw=0 pitch=0 hp=100 w=weapon_portalgun rf=500 rl=300 rr=300 fd=0 ents=prop_weighted_cube,200,0,0,1")
pl.step(o)
for _ in range(60):
    if br.intent() == "use": break
    time.sleep(0.05)
assert br.intent() == "use", seen
assert seen["auth"] == "Bearer sk-test" and seen["path"] == "/v1/chat/completions" and seen["model"] == "gpt-x", seen
for _ in range(4):
    br.observe(o, pl.profile); time.sleep(0.15)
assert br.intent() in (None, "use")
# dead API must fail soft
dead = OpenAIBrain("http://127.0.0.1:9/v1", "m", "k", interval=0.0, timeout=1.0)
for _ in range(5):
    dead.observe(o, pl.profile); time.sleep(0.3)
assert dead.intent() is None
httpd.shutdown()
print("openai-compatible brain ok")
