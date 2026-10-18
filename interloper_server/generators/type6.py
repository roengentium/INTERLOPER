import math
import random
from typing import List, Optional, Tuple

from recording import CommitFormat, Event, EventType, Recording, Tick
from repo import Repo
from generators.type2 import MapBounds

Vec3 = Tuple[float, float, float]

# Type 6 ("Editor behavior" / "Crasher"): a noclip camera that teleports and
# flies around at inhuman speed, snaps onto an object of interest with perfect
# precision, and deletes it. Lore points modelled here:
#   - teleporting + rapid flying, camera movement that is clearly not human
#   - machine-precision aim: the camera snaps in one tick and then holds
#     perfectly still (zero jitter) until the delete
#   - sometimes it locks onto a MOVING object
#   - sometimes the target is not deletable (e.g. a moving texture): the
#     deletion sound spams while the camera flickers in place
#   - rarely a "void trip": runaway-speed flight out of the map
#
# The generator knows nothing about real entities, so it cannot aim at one.
# It emits intent through ENTITY_DELETE events and the client resolves it:
#   param b"lock"         client picks a nearby valid target and aims at it
#   param b"lock:moving"  same, but prefer a moving target
#   param b"delete"       client deletes (or kills, for NPCs) the locked target,
#                         or whatever is under the crosshair if nothing is locked
# SOUND param is the real sound path (friends/friend_online.wav); a client may
# remap it, e.g. to a sound packaged with its own mod.
# The recorded angles during a lock are placeholders: a client that can find
# entities overrides them, a client that cannot just plays them.

TICKRATE = 66.0
MIN_SECONDS = 25.0     # not specified by the lore; a tunable guess
MAX_SECONDS = 75.0
REACH = 1800.0         # how far (units) the camera roams from its start point
FLY_SPEED = (25.0, 60.0)       # units per tick, roughly 1650-4000 units/s
HOLD_TICKS = (6, 40)
TELEPORT_CHANCE = 0.45
FAIL_CHANCE = 0.18
VOID_CHANCE = 0.06
VOID_MIN_SECONDS = 10.0
MOVING_CHANCE = 0.5

SOUND_DELETE = b"friends/friend_online.wav"   # the deletion sound from the lore


def _angles_toward(frm: Vec3, to: Vec3) -> Tuple[float, float]:
    """(pitch, yaw) looking from frm to to. Source pitch is positive looking down."""
    dx, dy, dz = to[0] - frm[0], to[1] - frm[1], to[2] - frm[2]
    yaw = math.degrees(math.atan2(dy, dx)) % 360.0
    pitch = -math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return max(-89.0, min(89.0, pitch)), yaw


def _approach_angle(cur: float, target: float, frac: float) -> float:
    diff = (target - cur + 180.0) % 360.0 - 180.0
    return (cur + diff * frac) % 360.0


def generate_type6(
    bounds: MapBounds,
    min_seconds: float = MIN_SECONDS,
    max_seconds: float = MAX_SECONDS,
    seed: Optional[int] = None,
    origin: Optional[Vec3] = None,
    tickrate: float = TICKRATE,
) -> Recording:
    rng = random.Random(seed)
    total = max(2, int(rng.uniform(min_seconds, max_seconds) * tickrate))

    if origin is None:
        origin = (
            rng.uniform(bounds.mins[0], bounds.maxs[0]),
            rng.uniform(bounds.mins[1], bounds.maxs[1]),
            rng.uniform(bounds.mins[2], bounds.maxs[2]),
        )
    center = origin

    # Roaming box: the map bounds intersected with a box around the start.
    lo = tuple(max(bounds.mins[a], center[a] - (REACH if a < 2 else REACH * 0.4)) for a in range(3))
    hi = tuple(min(bounds.maxs[a], center[a] + (REACH if a < 2 else REACH * 0.4)) for a in range(3))
    lo = tuple(min(lo[a], center[a]) for a in range(3))
    hi = tuple(max(hi[a], center[a]) for a in range(3))

    def rand_point() -> Vec3:
        return (rng.uniform(lo[0], hi[0]), rng.uniform(lo[1], hi[1]), rng.uniform(lo[2], hi[2]))

    ticks: List[Tick] = []
    events: List[Event] = []

    def emit(p: Vec3, pitch: float, yaw: float) -> None:
        ticks.append(Tick(
            origin=(p[0], p[1], p[2]),
            angles=(max(-89.0, min(89.0, pitch)), yaw % 360.0, 0.0),
            buttons=0,
            move=(0.0, 0.0, 0.0),
        ))

    def ev(kind: EventType, param: bytes = b"") -> None:
        # Fires together with the next tick that gets emitted.
        events.append(Event(len(ticks), kind, param))

    pos = center
    pitch, yaw = 0.0, rng.uniform(0.0, 360.0)
    emit(pos, pitch, yaw)
    voided = False
    force_teleport = False

    while len(ticks) < total:
        # --- 1. get somewhere new: teleport, or fly there very fast ---
        if force_teleport or rng.random() < TELEPORT_CHANCE:
            force_teleport = False
            pos = rand_point()
            yaw = rng.uniform(0.0, 360.0)
            pitch = rng.uniform(-30.0, 30.0)
            emit(pos, pitch, yaw)
        else:
            dest = rand_point()
            speed = rng.uniform(*FLY_SPEED)
            dist = math.dist(pos, dest)
            steps = max(2, int(dist / speed))
            tpitch, tyaw = _angles_toward(pos, dest)
            start = pos
            for s in range(1, steps + 1):
                f = s / steps
                pos = (start[0] + (dest[0] - start[0]) * f,
                       start[1] + (dest[1] - start[1]) * f,
                       start[2] + (dest[2] - start[2]) * f)
                yaw = _approach_angle(yaw, tyaw, 0.5)
                pitch += (tpitch - pitch) * 0.5
                emit(pos, pitch, yaw)

        # --- 2. rare void trip: runaway speed, out of the map ---
        if (not voided and len(ticks) / tickrate > VOID_MIN_SECONDS
                and rng.random() < VOID_CHANCE):
            voided = True
            force_teleport = True
            theta = rng.uniform(0.0, 2.0 * math.pi)
            direction = (math.cos(theta), math.sin(theta), rng.uniform(-0.3, 0.3))
            v = 30.0
            for _ in range(rng.randint(60, 200)):
                v *= 1.04
                pos = (pos[0] + direction[0] * v, pos[1] + direction[1] * v, pos[2] + direction[2] * v)
                emit(pos, pitch, yaw)
            continue

        if rng.random() < FAIL_CHANCE:
            # --- 3a. fixated on something undeletable: sound spam + flicker ---
            a = (rng.uniform(-35.0, 35.0), rng.uniform(0.0, 360.0))
            b = (a[0] + rng.uniform(-5.0, 5.0), a[1] + rng.uniform(-9.0, 9.0))
            use_b = False
            next_sound = 0
            for n in range(rng.randint(30, 90)):
                if rng.random() < 0.5:
                    use_b = not use_b
                if n >= next_sound:
                    ev(EventType.SOUND, SOUND_DELETE)
                    next_sound = n + rng.randint(2, 5)
                p = b if use_b else a
                emit(pos, p[0], p[1])
            pitch, yaw = a
        else:
            # --- 3b. lock on, hold dead still, delete ---
            pitch = rng.uniform(-35.0, 35.0)
            yaw = rng.uniform(0.0, 360.0)
            ev(EventType.ENTITY_DELETE, b"lock:moving" if rng.random() < MOVING_CHANCE else b"lock")
            hold = rng.randint(*HOLD_TICKS)
            for n in range(hold):
                if n == hold - 1:
                    ev(EventType.ENTITY_DELETE, b"delete")
                    ev(EventType.SOUND, SOUND_DELETE)
                emit(pos, pitch, yaw)    # identical every tick: machine precision
            for _ in range(rng.randint(0, 4)):
                emit(pos, pitch, yaw)

    ticks = ticks[:total]
    events = [e for e in events if e.tick_index < len(ticks)]
    events.sort(key=lambda e: e.tick_index)
    # Hold nothing down at the end (type 6 never uses buttons, but be explicit).
    ticks[-1].buttons = 0

    return Recording(
        demo_type=6,
        commit_format=CommitFormat.STANDARD,
        commit_number=Repo.new_commit_id(rng),
        ticks=ticks,
        events=events,
    )
