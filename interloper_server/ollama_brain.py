import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("interloper.brain")

INTENTS = ("explore", "collect", "use", "look", "fight", "avoid")

SYSTEM_PROMPT = (
    "You are the decision-maker for a casual video game player (like a YouTuber's let's play). "
    "You get a short description of the situation. Reply with ONLY a JSON object: "
    '{"intent": "<one of explore|collect|use|look|fight|avoid>", "why": "<five words>"}. '
    "explore = wander forward, collect = go pick up a nearby item, use = go press a button or "
    "pick up a prop, look = stop and look around, fight = attack visible enemies, avoid = keep "
    "away from enemies. Prefer variety. Do not explain anything else."
)


def _bearing(o, e) -> str:
    ang = (math.degrees(math.atan2(e.y - o.y, e.x - o.x)) - o.yaw + 180.0) % 360.0 - 180.0
    if abs(ang) < 25:
        return "ahead"
    if abs(ang) > 140:
        return "behind"
    return "left" if ang > 0 else "right"


def describe(o, profile) -> str:
    lines = [f"Game: {profile.name}. Health {o.hp}. Holding {o.weapon or 'nothing'}."]
    lines.append(f"Clear space: ahead {int(o.rf)}, left {int(o.rl)}, right {int(o.rr)}"
                 + (". Ledge ahead." if o.fd > 170 else "."))
    near = sorted(o.ents, key=lambda e: e.dist_from(o))[:6]
    if not near:
        lines.append("Nothing interesting nearby.")
    for e in near:
        lines.append(f"- {e.cls} {int(e.dist_from(o))} units {_bearing(o, e)}"
                     + ("" if e.vis else " (not visible)"))
    if o.hc:
        lines.append(f"Crosshair is on: {o.hc} at {int(o.hd)} units.")
    return "\n".join(lines)


class OllamaBrain:
    def __init__(self, model: str = "llama3", host: str = "http://127.0.0.1:11434",
                 interval: float = 2.5, timeout: float = 25.0, ttl: float = 7.0):
        self.model = model
        self.host = host.rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.ttl = ttl
        self._lock = threading.Lock()
        self._intent: Optional[str] = None
        self._intent_at = 0.0
        self._last_query = 0.0
        self._busy = False
        self._fails = 0
        self._disabled_until = 0.0
        self.queries = 0
        self.history = []   # (intent, why) for debug output

    # called by the policy on every observation; never blocks
    def observe(self, o, profile):
        now = time.monotonic()
        with self._lock:
            if self._busy or now < self._disabled_until or now - self._last_query < self.interval:
                return
            self._busy = True
            self._last_query = now
        prompt = describe(o, profile)
        threading.Thread(target=self._query, args=(prompt,), daemon=True).start()

    def intent(self, _game_time: float = 0.0) -> Optional[str]:
        with self._lock:
            if self._intent and time.monotonic() - self._intent_at <= self.ttl:
                return self._intent
        return None

    def _query(self, prompt: str):
        try:
            body = json.dumps({
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.9, "num_predict": 60},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }).encode()
            req = urllib.request.Request(self.host + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = data.get("message", {}).get("content", "")
            parsed = json.loads(text)
            intent = str(parsed.get("intent", "")).strip().lower()
            if intent not in INTENTS:
                raise ValueError(f"bad intent {intent!r}")
            with self._lock:
                self._intent = intent
                self._intent_at = time.monotonic()
                self._fails = 0
                self.queries += 1
                self.history.append((intent, str(parsed.get("why", ""))[:40]))
            log.info("brain: %s (%s)", intent, parsed.get("why", ""))
        except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            with self._lock:
                self._fails += 1
                if self._fails >= 3:
                    self._disabled_until = time.monotonic() + 60.0
                    self._fails = 0
                    log.warning("brain: Ollama not answering (%s); playing on rules for 60s", exc)
                else:
                    log.debug("brain query failed: %s", exc)
        finally:
            with self._lock:
                self._busy = False
