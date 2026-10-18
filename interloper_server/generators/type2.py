import random
from dataclasses import dataclass
from typing import Optional, Tuple

from recording import Recording, Tick, Event, EventType, CommitFormat
from repo import Repo

Vec3 = Tuple[float, float, float]


@dataclass
class MapBounds:
    mins: Vec3
    maxs: Vec3


def _pick_out_of_bounds_point(bounds: MapBounds, rng: random.Random) -> Vec3:
    mins, maxs = bounds.mins, bounds.maxs
    axis = rng.randint(0, 2)
    side = rng.choice((-1, 1))

    point = [
        rng.uniform(mins[0], maxs[0]),
        rng.uniform(mins[1], maxs[1]),
        rng.uniform(mins[2], maxs[2]),
    ]

    span = maxs[axis] - mins[axis]
    if span <= 0:
        span = 512.0  # sane fallback if bounds are degenerate
    offset = span * rng.uniform(0.05, 0.25)

    point[axis] = mins[axis] - offset if side < 0 else maxs[axis] + offset
    return tuple(point)


def generate_type2(
    bounds: MapBounds,
    min_ticks: int = 20,
    max_ticks: int = 90,
    seed: Optional[int] = None,
) -> Recording:
    rng = random.Random(seed)

    spawn = _pick_out_of_bounds_point(bounds, rng)
    duration = rng.randint(min_ticks, max_ticks)

    # The player's viewing angles are completely frozen the moment they enter Type 2
    pitch = rng.uniform(-10.0, 10.0)
    yaw = rng.uniform(0.0, 360.0)
    frozen_angles = (pitch, yaw, 0.0)

    ticks = []
    for _ in range(duration):
        # Strictly 0 inputs, 0 movement, and perfectly frozen camera angles
        ticks.append(Tick(
            origin=spawn,
            angles=frozen_angles,
            buttons=0,
            move=(0.0, 0.0, 0.0),
        ))

    # The sudden, unprompted KILL event at the final tick
    events = [Event(tick_index=duration - 1, event_type=EventType.KILL, param=b'')]

    return Recording(
        demo_type=2,
        commit_format=CommitFormat.STANDARD,
        commit_number=Repo.new_commit_id(rng),
        ticks=ticks,
        events=events,
    )
