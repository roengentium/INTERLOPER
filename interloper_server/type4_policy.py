"""
type4_policy.py

The Type 4 ("real gameplay") player: a rule-based "let's player" that is one
step above Type 3. Type 3 mashes inputs at random. This one has goals: it
walks somewhere on purpose, avoids walls and ledges, looks at things, picks
up items, presses buttons, fights what it can see, and sometimes stops to
look around like a YouTuber narrating. It is still human and imperfect:
reaction delay, aim wobble, getting stuck, bad weapon choices.

It is closed loop. The game plugin sends an observation about every 6 ticks
(text line, see parse_obs) and gets back an action (buttons + where to look).
Between observations the plugin keeps holding the last buttons and eases the
camera toward the requested angles, so the model/policy never has to run at
tick rate.

An optional LLM "brain" (ollama_brain.py) can set a high-level intent (explore,
collect, use, look, fight) every couple of seconds. The rules do the actual
steering, so a slow or missing model never freezes the player.
"""

import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from recording import Button
from type4_profiles import Profile, profile_for

B = Button


# -- observation / action types ---------------------------------------------

@dataclass
class Ent:
    cls: str
    x: float
    y: float
    z: float
    vis: bool

    def dist_from(self, o: "Obs") -> float:
        return math.sqrt((self.x - o.x) ** 2 + (self.y - o.y) ** 2 + (self.z - o.z) ** 2)

    def flat_dist_from(self, o: "Obs") -> float:
        return math.hypot(self.x - o.x, self.y - o.y)


@dataclass
class Obs:
    gt: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    hp: int = 100
    weapon: str = ""
    rf: float = 1024.0     # free distance straight ahead
    rl: float = 1024.0     # 45 degrees left
    rr: float = 1024.0     # 45 degrees right
    rl2: float = 1024.0    # 90 degrees left
    rr2: float = 1024.0    # 90 degrees right
    fd: float = 0.0        # floor distance below a point 64 units ahead (big = ledge)
    hc: str = ""           # class of what the crosshair is on
    hd: float = 0.0        # distance to it
    ents: List[Ent] = field(default_factory=list)


@dataclass
class Action:
    buttons: int = 0
    yaw: float = 0.0
    pitch: float = 0.0
    rate: float = 3.0      # max degrees the camera may turn per game tick
    cmd: Optional[str] = None
    end: bool = False

    def line(self) -> str:
        if self.end:
            return "END\n"
        out = f"ACT b={int(self.buttons)} yaw={self.yaw:.2f} pitch={self.pitch:.2f} rate={self.rate:.2f}"
        if self.cmd:
            out += f" cmd={self.cmd}"
        return out + "\n"


def _f(d: Dict[str, str], k: str, default: float = 0.0) -> float:
    try:
        return float(d.get(k, default))
    except (TypeError, ValueError):
        return default


def parse_obs(line: str) -> Obs:
    """OBS key=val key=val ... ents=cls,x,y,z,vis|cls,x,y,z,vis"""
    parts = line.strip().split()
    kv: Dict[str, str] = {}
    for p in parts[1:] if parts and parts[0] == "OBS" else parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k] = v
    o = Obs(
        gt=_f(kv, "gt"), x=_f(kv, "x"), y=_f(kv, "y"), z=_f(kv, "z"),
        yaw=_f(kv, "yaw"), pitch=_f(kv, "pitch"), hp=int(_f(kv, "hp", 100)),
        weapon=kv.get("w", ""),
        rf=_f(kv, "rf", 1024), rl=_f(kv, "rl", 1024), rr=_f(kv, "rr", 1024),
        rl2=_f(kv, "rl2", 1024), rr2=_f(kv, "rr2", 1024), fd=_f(kv, "fd", 0),
        hc=kv.get("hc", ""), hd=_f(kv, "hd"),
    )
    for chunk in kv.get("ents", "").split("|"):
        f = chunk.split(",")
        if len(f) >= 5:
            try:
                o.ents.append(Ent(f[0], float(f[1]), float(f[2]), float(f[3]), f[4] == "1"))
            except ValueError:
                pass
    return o


# -- helpers ------------------------------------------------------------------

def angdiff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def yaw_to(x0, y0, x1, y1) -> float:
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def pitch_to(dz: float, flat: float) -> float:
    """Source pitch is positive when looking DOWN."""
    return -math.degrees(math.atan2(dz, max(flat, 1.0)))


def _starts(cls: str, prefixes) -> bool:
    return any(cls.startswith(p) for p in prefixes)


# -- the player ------------------------------------------------------------------

class LetsPlayer:
    EYE_UP = 64.0

    def __init__(self, game: Optional[str] = None, seed: Optional[int] = None,
                 seconds: Optional[float] = None, brain=None, profile: Optional[Profile] = None):
        self.rng = random.Random(seed)
        self.profile = profile or profile_for(game)
        self.duration = seconds if seconds is not None else self.rng.uniform(75.0, 200.0)
        self.brain = brain
        self.t0: Optional[float] = None
        self.last_gt = 0.0

        self.heading: Optional[float] = None
        self.view_pitch = 0.0
        self.history = deque(maxlen=8)
        self.stuck_until = 0.0
        self.unstick_turn = 0.0
        self.unstick_jump = False
        self.turn_dir = 1.0
        self.turn_until = 0.0

        self.mode = "explore"
        self.mode_until = 0.0
        self.look_target: Optional[Ent] = None
        self.look_yaw = 0.0
        self.look_pitch = 0.0

        self.enemy_first_seen: Dict[int, float] = {}
        self.combat_strafe = 0.0
        self.combat_strafe_until = 0.0
        self.attack_on = False
        self.attack_switch_at = 0.0
        self.crouch_until = 0.0
        self.sprint_until = 0.0
        self.sprint_off_until = 0.0

        self.next_look_around = 0.0
        self.next_weapon_cmd = 0.0
        self.next_reload = 0.0
        self.next_jump = 0.0
        self.next_use = 0.0
        self.used: Dict[int, float] = {}
        self.ignored: Dict[int, float] = {}       # pickups we gave up on -> ignore until this time
        self.item_arrived: Dict[int, float] = {}  # when we first got within reach of a pickup
        self.carrying_until = 0.0
        self.next_portal = 0.0
        self.portal_flip = False
        self.pending_cmd: Optional[str] = None
        self.stats = {"obs": 0, "unstuck": 0, "fights": 0, "pickups": 0, "uses": 0, "looks": 0, "portals": 0}

    # -- main entry ------------------------------------------------------

    LEDGE = 170.0   # floor further than this below the probe point = a drop

    def step(self, o: Obs) -> Action:
        act = self._decide(o)
        if act.end:
            return act
        return self._safety(o, act)

    def _safety(self, o: Obs, act: Action) -> Action:
        """Last word on every action: never walk or sprint off a ledge, whatever
        mode (fight, pickup, carry, look-around) asked for it."""
        if o.fd <= self.LEDGE:
            return act
        move_mask = int(B.FORWARD) | int(B.SPEED) | int(B.MOVELEFT) | int(B.MOVERIGHT) | int(B.JUMP)
        if act.buttons & move_mask:
            act.buttons = (int(act.buttons) & ~move_mask) | int(B.BACK)
            if self.turn_until < o.gt:
                left_open = o.rl + o.rl2 * 0.5
                right_open = o.rr + o.rr2 * 0.5
                self.turn_dir = 1.0 if left_open >= right_open else -1.0
                self.turn_until = o.gt + 0.5
            act.yaw = o.yaw + self.turn_dir * 110.0
            act.rate = 9.0
            self.heading = act.yaw
        return act

    def _decide(self, o: Obs) -> Action:
        if self.t0 is None:
            self.t0 = o.gt
            self.heading = o.yaw
            self.view_pitch = o.pitch
            self.next_look_around = o.gt + self.rng.uniform(6, 14)
            self.next_weapon_cmd = o.gt + self.rng.uniform(10, 25)
            self.next_portal = o.gt + self.rng.uniform(2, 5)
        self.stats["obs"] += 1
        self.last_gt = o.gt
        now = o.gt

        if now - self.t0 >= self.duration or o.hp <= 0:
            return Action(end=True)

        if self.brain is not None:
            self.brain.observe(o, self.profile)

        self.history.append((now, o.x, o.y))
        act = Action(yaw=o.yaw, pitch=o.pitch)
        act.rate = 2.5
        buttons = 0

        # 1. stuck recovery overrides everything else.
        if now < self.stuck_until:
            return self._unstick(o, act)
        if self._is_stuck(o):
            self.stuck_until = now + self.rng.uniform(0.5, 0.9)
            self.unstick_turn = self.rng.choice((-1, 1)) * self.rng.uniform(70, 170)
            self.unstick_jump = self.rng.random() < 0.5
            self.stats["unstuck"] += 1
            self.history.clear()
            return self._unstick(o, act)

        # 2. choose what to do.
        enemies = self._enemies(o)
        intent = self.brain.intent(now) if self.brain is not None else None

        if o.hp < 30 and enemies and self.profile.can_fight:
            return self._retreat(o, act, enemies)
        if enemies and self.profile.can_fight and intent != "avoid":
            return self._fight(o, act, enemies)
        self._forget_enemies()

        if now < self.carrying_until:
            return self._carry(o, act)

        item = self._pick_item(o, intent)
        if item is not None:
            key = int(item.x) * 31 + int(item.y)
            if item.flat_dist_from(o) < 70.0:
                # Standing on a pickup that doesn't go away (the gun we already hold, something
                # we can't take): after a moment give up on it so we don't freeze there forever.
                first = self.item_arrived.setdefault(key, now)
                if now - first > 1.2:
                    self.ignored[key] = now + 120.0
                    self.item_arrived.pop(key, None)
                    item = None
            else:
                self.item_arrived.pop(key, None)
        if item is not None:
            return self._go_to(o, act, item, reach=70.0, tag="pickups")

        usable = self._pick_usable(o, intent)
        if usable is not None:
            return self._interact(o, act, usable)

        if now >= self.next_look_around or intent == "look":
            if self.mode != "look":
                self._start_look(o)
        if self.mode == "look" and now < self.mode_until:
            return self._look_around(o, act)
        if self.mode == "look":
            self.mode = "explore"

        return self._explore(o, act)

    # -- perception ------------------------------------------------------

    def _is_stuck(self, o: Obs) -> bool:
        if len(self.history) < 6 or self.mode == "look":
            return False
        t0, x0, y0 = self.history[0]
        if o.gt - t0 < 0.45:
            return False
        return math.hypot(o.x - x0, o.y - y0) < 10.0 and self._wants_to_move

    _wants_to_move = True

    def _enemies(self, o: Obs) -> List[Ent]:
        if not self.profile.can_fight:
            return []
        out = []
        for e in o.ents:
            if not e.vis or _starts(e.cls, self.profile.friendly_prefixes):
                continue
            if _starts(e.cls, self.profile.hostile_prefixes) and e.dist_from(o) < 1400:
                out.append(e)
        out.sort(key=lambda e: e.dist_from(o))
        return out

    def _forget_enemies(self):
        if self.enemy_first_seen and self.last_gt - max(self.enemy_first_seen.values()) > 3.0:
            self.enemy_first_seen.clear()
        self.attack_on = False

    def _pick_item(self, o: Obs, intent) -> Optional[Ent]:
        if not self.profile.item_prefixes:
            return None
        best, bd = None, 1e9
        limit = 1100.0 if intent == "collect" else 650.0
        for e in o.ents:
            if not _starts(e.cls, self.profile.item_prefixes):
                continue
            if o.weapon and e.cls == o.weapon:          # that's the weapon in our hands
                continue
            if self.ignored.get(int(e.x) * 31 + int(e.y), 0.0) > o.gt:
                continue
            d = e.flat_dist_from(o)
            if d < limit and e.vis and d < bd and abs(e.z - o.z) < 90:
                best, bd = e, d
        return best

    def _pick_usable(self, o: Obs, intent) -> Optional[Ent]:
        prefixes = self.profile.use_prefixes + self.profile.carry_prefixes
        if not prefixes:
            return None
        best, bd = None, 1e9
        limit = 900.0 if intent == "use" else 450.0
        for e in o.ents:
            if not _starts(e.cls, prefixes) or not e.vis:
                continue
            key = int(e.x) * 31 + int(e.y)
            if self.used.get(key, 0) > o.gt:
                continue
            d = e.flat_dist_from(o)
            if d < limit and d < bd:
                best, bd = e, d
        return best

    # -- behaviors ------------------------------------------------------------

    def _unstick(self, o: Obs, act: Action) -> Action:
        remaining = self.stuck_until - o.gt
        act.yaw = o.yaw + self.unstick_turn
        act.rate = 7.0
        b = B.BACK if remaining > 0.25 else B.FORWARD
        if self.unstick_jump and remaining < 0.3:
            b |= B.JUMP
        act.buttons = int(b)
        self.heading = act.yaw
        return act

    def _explore(self, o: Obs, act: Action) -> Action:
        now = o.gt
        self.mode = "explore"
        if self.heading is None:
            self.heading = o.yaw

        # lazy wandering: slow random drift of the heading
        self.heading += self.rng.uniform(-1.4, 1.4)
        if self.rng.random() < 0.02:
            self.heading += self.rng.uniform(-60, 60)

        buttons = B.FORWARD
        blocked = o.rf < 110 or (o.rl < 70 and o.rr < 70)
        ledge = o.fd > 170
        if blocked or ledge:
            if now >= self.turn_until:
                # turn toward whichever side is more open
                left_open = o.rl + o.rl2 * 0.5
                right_open = o.rr + o.rr2 * 0.5
                self.turn_dir = 1.0 if left_open >= right_open else -1.0
                if abs(left_open - right_open) < 40:
                    self.turn_dir = self.rng.choice((-1.0, 1.0))
                self.turn_until = now + self.rng.uniform(0.35, 0.8)
            self.heading += self.turn_dir * 14.0
            if o.rf < 50 or ledge:
                buttons = 0
        elif o.rl < 55:
            self.heading -= 6.0
        elif o.rr < 55:
            self.heading += 6.0

        # sprint now and then
        if self.profile.sprint:
            if now >= self.sprint_until and now >= self.sprint_off_until and self.rng.random() < 0.01:
                self.sprint_until = now + self.rng.uniform(1.5, 4.0)
                self.sprint_off_until = self.sprint_until + self.rng.uniform(3, 8)
            if now < self.sprint_until and o.rf > 250:
                buttons |= B.SPEED

        # hop small obstacles / for fun
        if now >= self.next_jump and (o.rf < 80 or self.rng.random() < 0.004):
            buttons |= B.JUMP
            self.next_jump = now + self.rng.uniform(1.0, 3.0)

        # look slightly down the path, occasionally glance at points of interest
        pitch = 4.0 + self.rng.uniform(-3, 3)
        act.rate = 2.4
        self.heading %= 360.0
        act.yaw = self.heading
        act.pitch = pitch

        # portal gun players fire portals at walls while walking
        if self.profile.portal_gun:
            buttons = self._portal_fire(o, buttons)

        # idle weapon fiddling
        self._idle_weapon(o, act)
        act.buttons = int(buttons)
        return act

    def _portal_fire(self, o: Obs, buttons: int) -> int:
        now = o.gt
        if now >= self.next_portal and o.rf < 900 and o.hc in ("worldspawn", "func_brush", "prop_dynamic", "func_wall", "prop_static", "prop_dynamic_override"):
            self.portal_flip = not self.portal_flip
            self.next_portal = now + self.rng.uniform(2.5, 7.0)
            self.stats["portals"] += 1
            return int(buttons | (B.ATTACK if self.portal_flip else B.ATTACK2))
        return int(buttons)

    def _idle_weapon(self, o: Obs, act: Action):
        now = o.gt
        if self.profile.weapon_cmds and now >= self.next_weapon_cmd:
            act.cmd = self.rng.choice(self.profile.weapon_cmds)
            self.next_weapon_cmd = now + self.rng.uniform(12, 35)
        elif now >= self.next_reload and self.profile.can_fight:
            act.buttons |= int(B.RELOAD)
            self.next_reload = now + self.rng.uniform(15, 40)

    def _start_look(self, o: Obs):
        self.mode = "look"
        self.stats["looks"] += 1
        now = o.gt
        self.mode_until = now + self.rng.uniform(1.6, 3.8)
        self.next_look_around = self.mode_until + self.rng.uniform(8, 20)
        # prefer looking at something interesting, otherwise sweep the room
        pool = [e for e in o.ents if e.vis and e.dist_from(o) < 900]
        self.look_target = self.rng.choice(pool) if pool and self.rng.random() < 0.75 else None
        if self.look_target is None:
            self.look_yaw = o.yaw + self.rng.uniform(-120, 120)
            self.look_pitch = self.rng.uniform(-18, 22)

    def _look_around(self, o: Obs, act: Action) -> Action:
        if self.look_target is not None:
            e = self.look_target
            act.yaw = yaw_to(o.x, o.y, e.x, e.y)
            act.pitch = pitch_to(e.z - (o.z + self.EYE_UP), e.flat_dist_from(o))
        else:
            act.yaw = self.look_yaw
            act.pitch = self.look_pitch
        act.rate = 1.1
        # the narrator slowly walks while looking, or stands still
        act.buttons = int(B.FORWARD) if (o.rf > 220 and o.fd < 120 and self.rng.random() < 0.5) else 0
        self.heading = o.yaw
        return act

    def _go_to(self, o: Obs, act: Action, e: Ent, reach: float, tag: str) -> Action:
        flat = e.flat_dist_from(o)
        act.yaw = yaw_to(o.x, o.y, e.x, e.y)
        act.pitch = pitch_to(e.z - (o.z + self.EYE_UP), flat) * 0.6
        act.rate = 3.2
        b = B.FORWARD if flat > reach * 0.5 else 0
        if o.rf < 70 and flat > o.rf:
            b |= B.JUMP
        if flat < reach:
            self.stats[tag] += 1
        act.buttons = int(b)
        self.heading = act.yaw
        return act

    def _interact(self, o: Obs, act: Action, e: Ent) -> Action:
        flat = e.flat_dist_from(o)
        key = int(e.x) * 31 + int(e.y)
        act = self._go_to(o, act, e, reach=85.0, tag="uses")
        if flat < 95.0 and o.gt >= self.next_use:
            act.buttons |= int(B.USE)
            self.next_use = o.gt + 0.7
            self.used[key] = o.gt + self.rng.uniform(8, 20)
            if _starts(e.cls, self.profile.carry_prefixes):
                self.carrying_until = o.gt + self.rng.uniform(4, 10)
        return act

    def _carry(self, o: Obs, act: Action) -> Action:
        """Holding a prop (Portal cube): keep walking, press use again to drop it."""
        act = self._explore(o, act)
        act.buttons |= int(B.FORWARD)
        if self.carrying_until - o.gt < 0.15:
            act.buttons |= int(B.USE)
            self.carrying_until = 0.0
        return act

    def _retreat(self, o: Obs, act: Action, enemies: List[Ent]) -> Action:
        e = enemies[0]
        away = yaw_to(e.x, e.y, o.x, o.y)
        act.yaw = away + self.rng.uniform(-25, 25)
        act.pitch = 0.0
        act.rate = 6.0
        act.buttons = int(B.FORWARD | B.SPEED) if o.rf > 100 else int(B.FORWARD)
        self.heading = act.yaw
        return act

    def _fight(self, o: Obs, act: Action, enemies: List[Ent]) -> Action:
        now = o.gt
        e = enemies[0]
        key = id(e.cls) ^ int(e.x) ^ int(e.y)
        first = self.enemy_first_seen.setdefault(key, now)
        if len(self.enemy_first_seen) > 16:
            self.enemy_first_seen.clear()
        # human reaction time: keep exploring for a moment after first sighting
        if not hasattr(self, "_react_delay") or self._react_delay is None:
            self._react_delay = self.rng.uniform(0.3, 0.75)
        if now - first < self._react_delay:
            return self._explore(o, act)
        self.stats["fights"] += 1 if self.mode != "fight" else 0
        self.mode = "fight"

        dist = e.dist_from(o)
        flat = e.flat_dist_from(o)
        aim_yaw = yaw_to(o.x, o.y, e.x, e.y) + self.rng.gauss(0, 1.6)
        aim_pitch = pitch_to(e.z + 24 - (o.z + self.EYE_UP), flat) + self.rng.gauss(0, 1.2)
        act.yaw, act.pitch, act.rate = aim_yaw, aim_pitch, 5.5

        buttons = 0
        err = abs(angdiff(aim_yaw, o.yaw))
        if dist > 450:
            buttons |= B.FORWARD
        elif dist < 160:
            buttons |= B.BACK
        # strafe
        if now >= self.combat_strafe_until:
            self.combat_strafe = self.rng.choice((-1, 0, 1))
            self.combat_strafe_until = now + self.rng.uniform(0.4, 1.2)
        if self.combat_strafe < 0:
            buttons |= B.MOVELEFT
        elif self.combat_strafe > 0:
            buttons |= B.MOVERIGHT
        if now < self.crouch_until:
            buttons |= B.DUCK
        elif self.rng.random() < 0.01:
            self.crouch_until = now + self.rng.uniform(0.4, 1.0)
        # burst fire when roughly on target
        if now >= self.attack_switch_at:
            self.attack_on = not self.attack_on
            self.attack_switch_at = now + (self.rng.uniform(0.3, 0.9) if self.attack_on else self.rng.uniform(0.2, 0.6))
        if self.attack_on and err < 9.0 and dist < self.profile.attack_range:
            buttons |= B.ATTACK
        # sometimes swap weapons mid fight, sometimes badly
        if self.profile.weapon_cmds and now >= self.next_weapon_cmd and self.rng.random() < 0.05:
            act.cmd = self.rng.choice(self.profile.weapon_cmds)
            self.next_weapon_cmd = now + self.rng.uniform(8, 20)
        act.buttons = int(buttons)
        self.heading = o.yaw
        return act
