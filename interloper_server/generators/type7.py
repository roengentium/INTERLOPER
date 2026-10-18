import random
from typing import Optional, Tuple

from recording import CommitFormat, Event, EventType, Recording, Tick
from generators.type2 import MapBounds

Vec3 = Tuple[float, float, float]

# Type 7 ("Cubemap idle"): the player is spawned at a random location, stands
# completely still holding the cubemap reflection test weapon (weapon_cubemap),
# and repeatedly plays one of the chime sounds. Lore points modelled here:
#   - random spawn location
#   - perfectly stationary: no input, fixed view
#   - weapon_cubemap equipped (its six reflective balls are the "cubes")
#   - one chime sound, chosen per demo, played over and over
#   - the pull reads as "COMMITTED TODAY" instead of a ##-##-## number
#
# The chime is sent symbolically as b"chime:<n>". Each client maps n onto its
# own list of chime sound files (n wraps around the list length), so the exact
# file names live in one place per client, not in the recordings.
#
# Client requirement: the map needs built cubemaps, or the balls have nothing
# to reflect. Run `buildcubemaps` once per map before playing Type 7.

TICKRATE = 66.0
MIN_SECONDS = 20.0      # the lore gives no length; a tunable guess
MAX_SECONDS = 60.0
CHIME_INTERVAL_SECONDS = (2.5, 6.0)
CHIME_VARIANTS = 8      # n is drawn from 0..CHIME_VARIANTS-1


def generate_type7(
    bounds: MapBounds,
    min_seconds: float = MIN_SECONDS,
    max_seconds: float = MAX_SECONDS,
    seed: Optional[int] = None,
    origin: Optional[Vec3] = None,
    tickrate: float = TICKRATE,
) -> Recording:
    rng = random.Random(seed)
    total = max(2, int(rng.uniform(min_seconds, max_seconds) * tickrate))

    # "Spawned in a random location". A client that knows real spawn points
    # may ignore this; a client that plays positions uses it as given.
    spawn = (
        rng.uniform(bounds.mins[0], bounds.maxs[0]),
        rng.uniform(bounds.mins[1], bounds.maxs[1]),
        rng.uniform(bounds.mins[2], bounds.maxs[2]),
    )
    angles = (rng.uniform(-5.0, 15.0), rng.uniform(0.0, 360.0), 0.0)

    ticks = [Tick(origin=spawn, angles=angles, buttons=0, move=(0.0, 0.0, 0.0))
             for _ in range(total)]

    events = [Event(0, EventType.WEAPON_EQUIP, b"weapon_cubemap")]

    chime = b"chime:%d" % rng.randrange(CHIME_VARIANTS)
    interval = int(rng.uniform(*CHIME_INTERVAL_SECONDS) * tickrate)
    t = rng.randint(5, max(6, interval))        # first chime shortly after the start
    while t < total:
        events.append(Event(t, EventType.SOUND, chime))
        t += interval

    return Recording(
        demo_type=7,
        commit_format=CommitFormat.TODAY,
        commit_number="TODAY",
        ticks=ticks,
        events=events,
    )
