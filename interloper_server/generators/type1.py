import random
from typing import Optional, Tuple

from recording import Recording, Tick, CommitFormat
from repo import Repo
from generators.type2 import MapBounds

Vec3 = Tuple[float, float, float]

# Type 1 ("Twisting"): the camera swings around wildly for a second or two,
# nothing interesting happens, and the demo just ends. No sound, no KILL.
#
# Tuning knobs. Ticks are per GameFrame call, so at HL2's ~66 tick/s the
# default 66-132 range is roughly 1-2 seconds of playback.
FLICK_CHANCE = 0.35          # per-tick odds of picking a brand-new angular velocity
MAX_YAW_FLICK = 45.0         # degrees per tick
MAX_PITCH_FLICK = 20.0       # degrees per tick
POS_JITTER = 3.0             # units of random-walk per tick (erratic movement)
CALM_CHANCE = 0.25           # odds the swinging dies down partway through
MAX_PITCH = 89.0             # Source clamps pitch just inside +/-90


def _random_in_bounds_point(bounds: MapBounds, rng: random.Random) -> Vec3:
    return tuple(rng.uniform(bounds.mins[a], bounds.maxs[a]) for a in range(3))


def generate_type1(
    bounds: MapBounds,
    min_ticks: int = 66,
    max_ticks: int = 132,
    seed: Optional[int] = None,
    origin: Optional[Vec3] = None,
    flick: float = FLICK_CHANCE,
    yaw_speed: float = MAX_YAW_FLICK,
    pitch_speed: float = MAX_PITCH_FLICK,
    jitter: float = POS_JITTER,
    calm: Optional[bool] = None,
    pos: str = "player",
    lead: int = 0,
    tail: int = 0,
) -> Recording:
    """origin: where the player should be. The current wire protocol only sends
    map bounds, so the server doesn't know the player's position; with origin=None
    this picks a random in-bounds point (likely inside solid geometry, which is
    fine under noclip but looks odd). Sending the player's real origin in the
    request block would let type 1 twist the camera exactly where they stand."""
    rng = random.Random(seed)

    # pos: "player" = twist where the player stands (falls back to random if
    # the origin is unknown), "random" = random point inside the map bounds,
    # "void" = a point just outside the map bounds (like type 2).
    mode = pos
    if mode == "void":
        from generators.type2 import _pick_out_of_bounds_point
        start = _pick_out_of_bounds_point(bounds, rng)
    elif mode == "player" and origin is not None:
        start = origin
    else:
        start = _random_in_bounds_point(bounds, rng)
    pos = list(start)
    if max_ticks < min_ticks:
        min_ticks, max_ticks = max_ticks, min_ticks
    duration = rng.randint(min_ticks, max_ticks)

    pitch = rng.uniform(-30.0, 30.0)
    yaw = rng.uniform(0.0, 360.0)
    pitch_vel = 0.0
    yaw_vel = 0.0

    # Lore: "sometimes it may break out of the wild swinging". In those runs
    # the camera decays to a stop partway through and just holds.
    do_calm = (rng.random() < CALM_CHANCE) if calm is None else calm
    calm_at = int(duration * rng.uniform(0.3, 0.8)) if do_calm else None

    ticks = []
    for i in range(duration):
        if calm_at is not None and i >= calm_at:
            yaw_vel *= 0.6
            pitch_vel *= 0.6
        elif rng.random() < flick or (yaw_vel == 0.0 and pitch_vel == 0.0):
            yaw_vel = rng.uniform(-yaw_speed, yaw_speed)
            pitch_vel = rng.uniform(-pitch_speed, pitch_speed)

        yaw = (yaw + yaw_vel) % 360.0
        pitch += pitch_vel
        if pitch > MAX_PITCH or pitch < -MAX_PITCH:
            pitch = max(-MAX_PITCH, min(MAX_PITCH, pitch))
            pitch_vel = -pitch_vel * 0.5  # bounce off the clamp instead of pinning

        if calm_at is None or i < calm_at:
            for a in range(3):
                pos[a] += rng.uniform(-jitter, jitter)

        ticks.append(Tick(
            origin=tuple(pos),
            angles=(pitch, yaw, 0.0),
            buttons=0,
            move=(0.0, 0.0, 0.0),
        ))

    # Optional idle padding so the .dem has frames before/after the swing.
    if ticks and (lead or tail):
        first, last = ticks[0], ticks[-1]
        ticks = ([Tick(first.origin, first.angles, 0, (0.0, 0.0, 0.0))] * max(0, lead)
                 + ticks
                 + [Tick(last.origin, last.angles, 0, (0.0, 0.0, 0.0))] * max(0, tail))

    return Recording(
        demo_type=1,
        commit_format=CommitFormat.STANDARD,
        commit_number=Repo.new_commit_id(rng),
        ticks=ticks,
        events=[],
    )
