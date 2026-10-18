import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from recording import Button, CommitFormat, Event, EventType, Recording, Tick
from repo import Repo
from generators.type2 import MapBounds

Vec3 = Tuple[float, float, float]

# Type 3 ("Aimless gameplay"): minutes of seemingly random play. The character
# runs around, shoots at nothing, jumps, switches weapons, walks into walls.
# No purpose and no KILL event; the demo just stops.
#
# Unlike types 1 and 2, this generator does NOT dictate positions. It produces
# inputs (buttons + view angles) and the client feeds them to the real engine,
# so collisions, stairs and weapon fire behave normally. The origin stored in
# each tick is just the starting point and is ignored by a client that plays
# input-driven demos.

TICKRATE = 66.0
MIN_SECONDS = 90.0
MAX_SECONDS = 240.0
GAMEPAD_CHANCE = 0.2          # sloppy analog-stick feel instead of mouse feel

# Default loadout is Half-Life 2's. Other games would pass their own names.
HL2_LOADOUT = ("weapon_pistol", "weapon_smg1", "weapon_shotgun")
HL2_SLOTS = ("slot1", "slot2", "slot2", "slot3", "slot3", "slot4")

# Per-game loadouts, picked with the `game` option (the HL2 plugin sends it
# automatically from the game folder name). TF2 has no `give`: the player's
# own class loadout is used, so only slot switches (primary/secondary/melee).
GAME_LOADOUTS = {
    "tf": ((), ("slot1", "slot1", "slot2", "slot2", "slot3")),
    "tf2": ((), ("slot1", "slot1", "slot2", "slot2", "slot3")),
    "hl1": (("weapon_glock", "weapon_mp5", "weapon_shotgun"), HL2_SLOTS),
    "portal": (("weapon_portalgun",), ("slot1", "slot2")),
    "portal2": (("weapon_portalgun",), ("slot1", "slot2")),
}
REGIVE_EVERY_SECONDS = (35.0, 60.0)      # re-give weapons to restock ammo
WEAPON_SWITCH_EVERY_SECONDS = (6.0, 25.0)

MAX_PITCH = 89.0

B = Button


@dataclass(frozen=True)
class Behavior:
    name: str
    weight: int
    seconds: Tuple[float, float]
    forward: float = 0.0          # chance the forward key is held for the segment
    back: float = 0.0
    strafe: float = 0.0
    sprint: float = 0.0
    crouch: float = 0.0
    fire: str = "none"            # "none" | "burst" | "hold"
    fidget_rate: float = 0.0      # short random key blips per second
    fidget_keys: Tuple[Button, ...] = ()
    turn: float = 1.0             # scale on camera motion, 0 freezes the view


BEHAVIORS = (
    Behavior("wander", 30, (3.0, 12.0), forward=0.85, strafe=0.15, sprint=0.3,
             fidget_rate=0.15, fidget_keys=(B.JUMP, B.USE)),
    Behavior("circle_strafe", 10, (3.0, 9.0), forward=0.5, strafe=1.0, turn=1.6),
    Behavior("spray", 14, (2.0, 8.0), forward=0.4, strafe=0.4, fire="hold",
             fidget_rate=0.1, fidget_keys=(B.RELOAD,), turn=1.2),
    Behavior("burst_fire", 14, (2.0, 8.0), forward=0.3, strafe=0.3, fire="burst",
             fidget_rate=0.1, fidget_keys=(B.RELOAD,)),
    Behavior("jump_spam", 8, (3.0, 9.0), forward=0.6, strafe=0.2,
             fidget_rate=2.5, fidget_keys=(B.JUMP,)),
    Behavior("crouch_creep", 6, (3.0, 9.0), forward=0.7, crouch=1.0, turn=0.6),
    Behavior("backpedal", 5, (2.0, 6.0), back=0.9, strafe=0.2, turn=0.4),
    Behavior("wall_push", 8, (3.0, 12.0), forward=1.0, turn=0.0),
    Behavior("idle_fidget", 10, (2.0, 7.0), fidget_rate=0.8,
             fidget_keys=(B.JUMP, B.USE, B.RELOAD, B.DUCK, B.ATTACK2), turn=0.6),
)


def _movement_mask(b: Behavior, rng: random.Random) -> int:
    mask = 0
    if rng.random() < b.forward:
        mask |= B.FORWARD
    elif rng.random() < b.back:
        mask |= B.BACK
    if rng.random() < b.strafe:
        mask |= rng.choice((B.MOVELEFT, B.MOVERIGHT))
    if mask and rng.random() < b.sprint:
        mask |= B.SPEED
    if rng.random() < b.crouch:
        mask |= B.DUCK
    return int(mask)


def _move_axes(buttons: int) -> Tuple[float, float, float]:
    fwd = (1.0 if buttons & B.FORWARD else 0.0) - (1.0 if buttons & B.BACK else 0.0)
    side = (1.0 if buttons & B.MOVERIGHT else 0.0) - (1.0 if buttons & B.MOVELEFT else 0.0)
    up = (1.0 if buttons & B.JUMP else 0.0) - (1.0 if buttons & B.DUCK else 0.0)
    return (fwd, side, up)


def generate_type3(
    bounds: MapBounds,
    min_seconds: float = MIN_SECONDS,
    max_seconds: float = MAX_SECONDS,
    seed: Optional[int] = None,
    origin: Optional[Vec3] = None,
    loadout: Tuple[str, ...] = HL2_LOADOUT,
    slots: Tuple[str, ...] = HL2_SLOTS,
    tickrate: float = TICKRATE,
    style: Optional[str] = None,
    game: Optional[str] = None,
) -> Recording:
    rng = random.Random(seed)

    if game and game.lower() in GAME_LOADOUTS:
        loadout, slots = GAME_LOADOUTS[game.lower()]

    if origin is None:
        origin = (
            (bounds.mins[0] + bounds.maxs[0]) / 2.0,
            (bounds.mins[1] + bounds.maxs[1]) / 2.0,
            bounds.mins[2] + 64.0,
        )

    if style is None:
        style = "gamepad" if rng.random() < GAMEPAD_CHANCE else "keyboard"
    gamepad = style == "gamepad"

    total = max(2, int(rng.uniform(min_seconds, max_seconds) * tickrate))

    # Camera model: yaw/pitch velocity eases toward a randomly re-picked target.
    smoothing = 0.06 if gamepad else 0.25
    drift = rng.uniform(-0.15, 0.15) if gamepad else 0.0   # worn stick drift
    fidget_mult = 2.0 if gamepad else 1.0

    yaw = rng.uniform(0.0, 360.0)
    pitch = rng.uniform(-10.0, 10.0)
    yaw_vel = 0.0
    yaw_target = 0.0
    pitch_target = 0.0
    next_retarget = 0

    # Segment / input state
    behavior = BEHAVIORS[0]
    segment_end = 0
    held = 0
    attack_until = -1
    attack_gap_until = -1
    blips: Dict[int, int] = {}   # button bit -> ticks remaining

    ticks: List[Tick] = []
    for i in range(total):
        if i >= segment_end:
            behavior = rng.choices(BEHAVIORS, weights=[x.weight for x in BEHAVIORS], k=1)[0]
            segment_end = i + int(rng.uniform(*behavior.seconds) * tickrate)
            held = _movement_mask(behavior, rng)

        # --- camera ---
        if i >= next_retarget:
            r = rng.random()
            sign = rng.choice((-1.0, 1.0))
            if behavior.turn == 0.0:
                yaw_target = 0.0
                pitch_target = pitch
            else:
                if r < 0.45:
                    yaw_target = 0.0
                elif r < 0.85:
                    yaw_target = sign * rng.uniform(0.5, 2.5) * behavior.turn
                else:
                    yaw_target = sign * rng.uniform(3.0, 8.0) * behavior.turn
                p = rng.random()
                if p < 0.7:
                    pitch_target = rng.uniform(-15.0, 15.0)
                elif p < 0.9:
                    pitch_target = rng.uniform(-40.0, 40.0)
                else:
                    pitch_target = rng.choice((-1.0, 1.0)) * rng.uniform(60.0, 85.0)
            next_retarget = i + rng.randint(15, 130)

        yaw_vel += (yaw_target - yaw_vel) * smoothing
        yaw = (yaw + yaw_vel + (drift if behavior.turn else 0.0)) % 360.0
        pitch += (pitch_target - pitch) * (0.04 if gamepad else 0.08)
        pitch = max(-MAX_PITCH, min(MAX_PITCH, pitch))

        # --- buttons ---
        buttons = held

        if behavior.fire != "none":
            if i >= attack_until and i >= attack_gap_until:
                if behavior.fire == "hold":
                    attack_until = i + rng.randint(20, 100)
                    attack_gap_until = attack_until + rng.randint(10, 60)
                else:
                    attack_until = i + rng.randint(3, 10)
                    attack_gap_until = attack_until + rng.randint(5, 20)
            if i < attack_until:
                buttons |= B.ATTACK

        if behavior.fidget_keys and rng.random() < behavior.fidget_rate * fidget_mult / tickrate:
            key = rng.choice(behavior.fidget_keys)
            blips[int(key)] = rng.randint(1, 2 if gamepad else 4)
        for bit in list(blips):
            buttons |= bit
            blips[bit] -= 1
            if blips[bit] <= 0:
                del blips[bit]

        if i == total - 1:
            buttons = 0   # leave nothing held down at the end

        ticks.append(Tick(
            origin=origin,
            angles=(pitch, yaw, 0.0),
            buttons=int(buttons),
            move=_move_axes(int(buttons)),
        ))

    # --- events ---
    events: List[Event] = []
    for n, weapon in enumerate(loadout):
        events.append(Event(n, EventType.WEAPON_EQUIP, weapon.encode("ascii")))

    t = int(rng.uniform(*REGIVE_EVERY_SECONDS) * tickrate)
    while t < total - 1:
        for n, weapon in enumerate(loadout):
            if t + n < total - 1:
                events.append(Event(t + n, EventType.WEAPON_EQUIP, weapon.encode("ascii")))
        t += int(rng.uniform(*REGIVE_EVERY_SECONDS) * tickrate)

    # Weapon switching: a "slotN" param is sent as a command, not given.
    t = int(rng.uniform(*WEAPON_SWITCH_EVERY_SECONDS) * tickrate) + len(loadout)
    while t < total - 1 and slots:
        events.append(Event(t, EventType.WEAPON_EQUIP, rng.choice(slots).encode("ascii")))
        t += int(rng.uniform(*WEAPON_SWITCH_EVERY_SECONDS) * tickrate)

    events.sort(key=lambda e: e.tick_index)

    return Recording(
        demo_type=3,
        commit_format=CommitFormat.STANDARD,
        commit_number=Repo.new_commit_id(rng),
        ticks=ticks,
        events=events,
    )
